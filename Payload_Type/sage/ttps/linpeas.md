---
name: LinPEAS
category: discovery
subcategories: [linux-sa, privesc-discovery, automated-enumeration]
tradecraft_tags: [linux, privesc, enumeration, automated, bash-script, poseidon, peass]
mitre_attack:
  - id: T1082
    name: System Information Discovery
  - id: T1087.001
    name: Account Discovery — Local Account
source:
  url: https://github.com/carlospolop/PEASS-ng/tree/master/linPEAS
  license: Unknown
  maintained: true
binary_type: python-script
binary_filename: linpeas.sh
supported_os: [linux, macos]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  LinPEAS executes hundreds of shell commands rapidly — process creation burst
  (many subprocess spawns) is detectable by auditd and Linux EDR (CrowdStrike
  Falcon for Linux). The script's file reads across /proc, /sys, /etc, and user
  directories generate unusual filesystem access patterns. On systems with auditd,
  the audit log shows a flood of execve() calls.
usage_examples:
  - description: Full LinPEAS run (all checks)
    args: "bash linpeas.sh | tee /tmp/linpeas.out"
  - description: Run with color output (for local review)
    args: "bash linpeas.sh -a 2>&1 | tee /tmp/linpeas_full.out"
  - description: Fast mode (skip time-consuming checks)
    args: "bash linpeas.sh -F"
  - description: Specific check only (network)
    args: "bash linpeas.sh -o SysInfo"
  - description: Upload and run via Poseidon
    args: "(upload linpeas.sh to /tmp/, chmod +x, execute via shell)"
opsec_notes: |
  LinPEAS is comprehensive but noisy — hundreds of subprocess spawns in seconds.
  Modern Linux EDR (CrowdStrike Falcon for Linux) detects this pattern with high
  confidence. For stealth: run targeted manual checks (sudo -l, find SUID) instead.
  LinPEAS is most appropriate for CTF, lab environments, or engagements where the
  operator needs a rapid full picture and detection risk is acceptable.
gotchas: |
  Bash script — executable on any Linux/macOS target with bash. Upload via Poseidon's
  upload command, chmod +x, then execute. Output is color-coded ANSI — pipe to `tee`
  to save while viewing. -F (fast mode) skips many enumeration steps that take time
  but also reduces coverage. Always review LinPEAS output manually — it generates
  many false positives; critical findings are color-highlighted (red = critical,
  yellow = interesting, green = informational).
related_ttps: [linux-privesc, poseidon, seatbelt]
alternatives: [linux-privesc-manual, linenum, linux-exploit-suggester]
common_args:
  -a:
    description: All checks including slow ones
    typical_values: [flag-only]
  -F:
    description: Fast mode (skip slow checks)
    typical_values: [flag-only]
  -o:
    description: Run only specific check category
    typical_values: [SysInfo, UserInfo, Network, Software, Processes]
last_updated: 2026-05-29
---

# LinPEAS

The Linux counterpart to WinPEAS — an automated shell script that runs comprehensive
privilege escalation enumeration checks across a Linux/macOS system. LinPEAS is the
fastest path to a complete privesc picture on a Linux target at the cost of significant
process-creation noise.

## Coverage Areas

- System information (kernel version, OS, architecture)
- Users, groups, sudo rights
- SUID/SGID binaries and capabilities
- Cron jobs and scheduled tasks
- Network configuration (interfaces, listening ports, ARP, routes)
- Installed software and services
- Container awareness (Docker, LXC, Kubernetes)
- Credentials in files and environment
- Interesting files (world-writable, recently modified, home directories)
- Processes and services
- Weak permissions on service files

## Output Interpretation

LinPEAS uses color coding:
- **Red/Yellow** = highly interesting or directly exploitable
- **Blue** = informational
- **Green** = low confidence or informational

Always review red/yellow findings first. Many findings require GTFOBins lookup
or additional manual verification.

## Via Poseidon

```
# Upload and execute:
Poseidon: upload /tmp/linpeas.sh (UUID from Mythic)
Poseidon: shell chmod +x /tmp/linpeas.sh && bash /tmp/linpeas.sh 2>&1 > /tmp/output.txt
Poseidon: download /tmp/output.txt
```
