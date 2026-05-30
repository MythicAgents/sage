---
name: DeepPass
category: credential-access
subcategories: [ml-credential-hunting, memory-credential-scan, file-credential-scan]
tradecraft_tags: [ml, deep-learning, credential-hunt, memory-scan, file-scan, ghostpack, dotnet, apollo-runnable]
mitre_attack:
  - id: T1552.001
    name: Unsecured Credentials — Credentials In Files
source:
  url: https://github.com/GhostPack/DeepPass
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: DeepPass.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  DeepPass scans process memory and files for credential patterns using a lightweight
  deep learning model embedded in the binary. Memory reads generate process-access events
  (Sysmon Event 10) if the model scans other processes. File reads generate file access
  events. Behavioral EDR may flag rapid bulk process memory scanning.
usage_examples:
  - description: Scan all running process memory for credentials
    args: "DeepPass.exe --processes"
  - description: Scan a specific process by PID
    args: "DeepPass.exe --pid <PID>"
  - description: Scan files on disk for credentials
    args: "DeepPass.exe --files C:\\Users\\"
  - description: Scan both memory and disk
    args: "DeepPass.exe --processes --files C:\\Users\\"
opsec_notes: |
  DeepPass is unique among credential hunting tools — it uses a lightweight neural network
  (embedded in the binary) to identify credential patterns in raw memory and files.
  This catches credential material that regex/keyword approaches miss (context-aware
  detection: "the 32-char hex string after 'password=' is likely a credential").
  Memory scanning of other processes requires appropriate privileges (same user or SYSTEM)
  and generates process access events. For large-scale deployment, disk scanning is less
  noisy than memory scanning all processes.
gotchas: |
  DeepPass embeds a trained ML model in the binary — this makes it large (~50MB+) compared
  to other GhostPack tools. The ML model may produce false positives (especially for UUIDs,
  random data) and false negatives. Manual review of output is required. The quality of
  findings depends on what's loaded in process memory at scan time — run soon after user
  activity (application use, web browsing) for best coverage.
related_ttps: [credential-hunting-checklist, seatbelt, snaffler, sharpchromium]
alternatives: [seatbelt-credentialhunt, snaffler-filescans, manual-grep]
common_args:
  --processes:
    description: Scan running process memory for credentials
    typical_values: [flag-only]
  --pid:
    description: Target specific process PID for memory scan
    typical_values: ["<PID>"]
  --files:
    description: Scan filesystem path for credentials in files
    typical_values: ["C:\\\\Users\\\\", "C:\\\\"]
last_updated: 2026-05-29
---

# DeepPass

GhostPack's machine-learning-powered credential hunter. Unlike traditional keyword-based
tools, DeepPass uses a lightweight deep learning model (embedded in the binary) to
identify credential material in process memory and files by recognizing contextual patterns
rather than fixed strings.

## What Makes DeepPass Different

```
Traditional approach (grep/regex):
  Scan for: "password", "passwd", "secret", "token"
  Miss: credentials stored without obvious labels
  Miss: encoded/obfuscated credentials
  Miss: credentials with unconventional context

DeepPass ML approach:
  Trained on thousands of credential examples
  Identifies: context around credential-like strings
  Finds: credentials that don't match keyword patterns
  Example: "AKIAIOSFODNN7EXAMPLE" → AWS access key (length + character pattern)
```

## Typical Findings

- Cleartext passwords in application configuration loaded in memory
- API keys in web application process memory
- Connection strings with embedded credentials
- Credentials in log files or temp files on disk
- Recently typed passwords still in memory buffer

## Comparison with Other Credential Hunters

| Tool | Method | What it finds well |
|------|--------|-------------------|
| Seatbelt | Known locations | Registry, Credential Manager, AutoLogon |
| SharpChromium | Browser-specific | Chrome/Edge saved passwords |
| Snaffler | File content | Files on shares with credential keywords |
| DeepPass | ML/memory | Anywhere in memory + files, no obvious label |

## Apollo-specific note
.NET assembly — Apollo inline_assembly compatible. The tool is large (~50MB with
embedded model) — factor this into Mythic upload and memory footprint considerations.
