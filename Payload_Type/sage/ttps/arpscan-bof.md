---
name: ARP Scan BOF / PortScan BOF
category: discovery
subcategories: [network-discovery, arp-scan, port-scan, bof]
tradecraft_tags: [arp, port-scan, network-discovery, bof, in-process, athena, host-discovery]
mitre_attack:
  - id: T1018
    name: Remote System Discovery
  - id: T1046
    name: Network Service Discovery
source:
  url: https://github.com/trustedsec/CS-Situational-Awareness-BOF
  license: BSD-3-Clause
  maintained: true
binary_type: bof
binary_filename: arpscan.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: true
detection_signal: |
  ARP probes generate network traffic visible to network monitoring tools. In-process
  (BOF) execution avoids the nmap.exe or similar child process, but the ARP packets
  themselves are visible on the network. Port scanning generates TCP connection attempts
  that are logged by firewalls and IDS.
usage_examples:
  - description: ARP scan a subnet for live hosts
    args: "execute-bof arpscan.x64.o 192.168.56.0/24"
  - description: TCP port scan for open services
    args: "execute-bof portscan.x64.o 192.168.56.0/24 22,80,443,445,3389,5985"
  - description: Athena built-in alternatives
    args: "(Athena) portscan --hosts 192.168.56.0/24 --ports 80,443,445,3389,5985"
opsec_notes: |
  ARP scanning is loud at the network level — generates many ARP requests in a short
  time. Prefer single-host checks over subnet sweeps in monitored environments.
  For network discovery, ARP scan should follow credential-based discovery (enumerate
  from AD, then validate connectivity) rather than blind scanning.
gotchas: |
  Apollo has no BOF runner — requires Athena. Athena's built-in portscan command
  provides equivalent functionality without a separate BOF. For Apollo, use PowerView's
  network-based enumeration or SharpMapExec's check mode to enumerate reachable hosts.
related_ttps: [trustedsec-bofs, sharphound, crackmapexec]
alternatives: [athena-portscan, powerview-netscan, crackmapexec-sweep]
common_args:
  cidr:
    description: Target CIDR range for ARP scan
    typical_values: ["192.168.56.0/24"]
    required: true
  ports:
    description: Comma-separated ports for port scan
    typical_values: ["22,80,443,445,3389,5985,8080"]
last_updated: 2026-05-29
---

# ARP Scan BOF / PortScan BOF

TrustedSec SA BOFs for in-process network discovery. Provides ARP scanning for live
host discovery and TCP port scanning — running entirely inside the C2 agent process
without spawning nmap, masscan, or similar child processes.

## Network Discovery Strategy

```
Step 1: AD-based host enumeration (quietest — no network traffic):
  SharpHound or SharpLdapSearch for computer objects

Step 2: ARP scan for live hosts (loud but fast):
  execute-bof arpscan.x64.o 192.168.56.0/24
  → Returns: live IP addresses

Step 3: Port scan live hosts for accessible services:
  execute-bof portscan.x64.o <targets> 80,443,445,3389,5985
  → Returns: open ports per host
  
Step 4: Service-specific exploitation based on findings
```

## Apollo Alternatives

```powershell
# PowerView test-AdminAccess for reachability:
Test-AdminAccess -ComputerName WINTERFELL

# SharpMapExec sweep mode:
SharpMapExec.exe smb /command:check /targets:192.168.56.0/24 /user:admin /pass:pass

# Athena built-in (if using Athena):
portscan --hosts 192.168.56.0/24 --ports 445,3389,5985
arpscan --cidr 192.168.56.0/24
```
