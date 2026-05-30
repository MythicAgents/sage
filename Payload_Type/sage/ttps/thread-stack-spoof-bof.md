---
name: Thread Stack Spoofing
category: defense-evasion
subcategories: [call-stack-spoof, thread-stack, evasion, anti-forensics]
tradecraft_tags: [thread-stack, call-stack, spoof, evasion, bof, sleep-mask, returnaddress-spoof]
mitre_attack:
  - id: T1027
    name: Obfuscated Files or Information
source:
  url: https://github.com/WithSecureLabs/CallStackSpoofer
  license: Unknown
  maintained: true
binary_type: bof
binary_filename: (various implementations)
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Thread stack spoofing creates return addresses that point to legitimate system DLLs
  rather than the C2 agent's shellcode. Detecting spoofed stacks requires reconstructing
  the actual return address chain vs the thread's stack contents — this is done by
  behavioral EDR during process scanning (e.g. CrowdStrike's periodic scanner).
  Spoofed stacks where return addresses point to unusual DLLs or offsets within DLLs
  (not matching function entry points) are detectable by intelligent stack unwinding.
usage_examples:
  - description: Spoof thread call stack during C2 agent sleep (nanodump has built-in --spoof-callstack)
    args: "execute-bof threadstackspoofer.x64.o"
  - description: nanodump built-in stack spoofing (applied during dump operation)
    args: "nanodump --spoof-callstack --write C:\\Windows\\Temp\\out.dmp"
opsec_notes: |
  Thread stack spoofing is most valuable during the C2 agent's sleep interval —
  when the agent is waiting for the next callback, its thread's call stack would
  normally reveal the C2 shellcode's return address. Stack spoofing replaces these
  with legitimate return addresses pointing into system DLLs, making the sleeping
  thread appear as a normal system thread.
  
  The spoofing is applied during the "sleep mask" phase (when the agent is obfuscating
  itself during sleep). Not all C2 frameworks support this; Cobalt Strike 4.x+ has
  built-in sleep mask support with stack spoofing options.
gotchas: |
  Apollo may not support sleep mask / stack spoofing natively. Athena via execute-bof
  can run stack spoofer BOFs. nanodump's --spoof-callstack applies to the dump
  operation specifically (hides nanodump in stack traces during the dump), not the
  entire agent's sleep. Full sleep-time stack spoofing requires C2 framework support.
related_ttps: [nanodump-bof-expanded, bofnet, trustedsec-bofs, inline-execute-pe]
alternatives: [sleep-mask-obfuscation, beacon-sleep-mask]
common_args: {}
last_updated: 2026-05-29
---

# Thread Stack Spoofing

A defense evasion technique that replaces the return addresses in a thread's call stack
with legitimate-looking addresses pointing into system DLLs. Most valuable when the C2
agent is sleeping — EDR scanning of the sleeping agent's call stack normally reveals
the shellcode return address, identifying the agent. Stack spoofing makes the sleeping
thread look like a normal Windows system thread.

## When Stack Spoofing Matters

```
C2 Agent sleeping (without stack spoof):
  Thread stack:
    → ntdll.NtWaitForSingleObject
    → WinSock_recv (legitimate)
    → 0x12345678 (C2 shellcode region)  ← obvious anomaly

C2 Agent sleeping (with stack spoof):
  Thread stack:
    → ntdll.NtWaitForSingleObject
    → kernel32.WaitForSingleObjectEx
    → kernelbase.WaitForSingleObjectEx  ← all look legitimate
```

## Sleep Mask Pattern

Full stack spoofing is part of a "sleep mask" — a technique where the agent:
1. Obfuscates its own memory before sleeping (XOR/AES encrypts itself)
2. Spoofs its thread stack
3. Sleeps until the next callback interval
4. Decrypts/restores itself to continue execution

This makes periodic memory scanning (CrowdStrike's LSASS scanner, memory scanner modules)
less effective.

## Availability in Sage's Toolkit

| Tool | Stack spoof support | Notes |
|------|--------------------|-|
| nanodump BOF | `--spoof-callstack` | Spoof during dump operation |
| Apollo | Limited/partial | Agent-level sleep mask support varies |
| Athena | Via BOF extension | `execute-bof threadstackspoofer.x64.o` |
| Cobalt Strike | Built-in (CS 4.x+ sleep mask) | Reference implementation |

## Practical Impact

Stack spoofing is most effective against:
- Memory scanners that analyze thread call stacks (periodic EDR scans)
- Forensic memory analysis tools (Volatility, Rekall)
- Incident response live analysis

It does NOT help against:
- Behavioral detection (what the agent does, not what it looks like at rest)
- Network traffic analysis
- Process creation events
