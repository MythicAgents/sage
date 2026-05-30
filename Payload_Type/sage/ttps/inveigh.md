---
name: Inveigh
category: coercion-relay
subcategories: [llmnr-poisoning, nbt-ns-poisoning, ntlm-capture, windows-side]
tradecraft_tags: [llmnr, nbt-ns, mdns, ntlm-capture, responder-dotnet, apollo-runnable]
mitre_attack:
  - id: T1557.001
    name: Adversary-in-the-Middle — LLMNR/NBT-NS Poisoning and SMB Relay
source:
  url: https://github.com/Kevin-Robertson/Inveigh
  license: BSD-3-Clause
  maintained: true
binary_type: .net-assembly
binary_filename: Inveigh.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: true
detection_signal: |
  Same as Responder — LLMNR/NBT-NS responses from unexpected hosts are anomalous and
  detectable by network monitoring. SMB server activity generates network connections to
  the attacking host. The NTLM hash captures themselves are visible as authentication
  attempts on network monitors. Some EDRs detect the promiscuous network binding behavior.
usage_examples:
  - description: Start LLMNR/NBT-NS poisoning and capture NTLMv2 hashes
    args: "Inveigh.exe"
  - description: Inveigh in console mode (interactive)
    args: "Inveigh.exe -ConsoleOutput 1"
  - description: Capture only (no relay)
    args: "Inveigh.exe -LLMNR Y -NBNS Y -ConsoleOutput 1 -FileOutput Y"
  - description: Run for a specific time and then stop
    args: "Inveigh.exe -RunTime 30"
opsec_notes: |
  Inveigh is the .NET equivalent of Responder — the critical difference is it runs
  from Windows (via Apollo inline_assembly or powershell_import) without needing Linux
  infrastructure. The NTLM hashes captured require offline cracking; for operational use,
  combine with in-process relay (Inveigh has relay capabilities) or ntlmrelayx from
  infrastructure. Running Inveigh from within Apollo is noisy at the network level.
gotchas: |
  Inveigh binds to network interfaces from within the compromised host — this is detectable
  by behavioral EDR (unexpected network service on a non-server machine). The captured
  NTLMv2 hashes require offline cracking — Sage cannot crack these. For relay functionality,
  Inveigh has built-in relay (InveighRelay) but the configuration is complex. In most cases,
  coercion-based attacks (Coercer + ntlmrelayx from Linux) are operationally cleaner.
related_ttps: [responder, ntlmrelayx, coercer, krbrelay]
alternatives: [responder, mitm6]
common_args:
  -ConsoleOutput:
    description: Enable console output (0=off, 1=full, 2=verbose)
    typical_values: [1]
  -LLMNR:
    description: Enable LLMNR poisoning
    typical_values: [Y, N]
  -NBNS:
    description: Enable NBT-NS poisoning
    typical_values: [Y, N]
  -FileOutput:
    description: Enable file output for captured hashes
    typical_values: [Y]
  -RunTime:
    description: Number of minutes to run before stopping
    typical_values: [30, 60]
last_updated: 2026-05-29
---

# Inveigh

Kevin Robertson's .NET LLMNR/NBT-NS/mDNS spoofing and NTLM capture tool — the Windows-side
equivalent of Responder. Unlike Responder (Python/Linux), Inveigh runs as a .NET assembly
from within a Windows host, making it deployable via Apollo's inline_assembly command.
It listens for broadcast name resolution queries, responds with the attacker's IP, and
captures NTLMv2 hashes from the resulting authentication attempts.

## Typical use cases
- LLMNR/NBT-NS poisoning and hash capture from within a compromised Windows host
- Windows-side alternative when Responder infrastructure isn't available
- Passive hash capture for later relay operations

## How Sage uses this
Inveigh is the Windows-side (Apollo-compatible) alternative to Responder. When Sage needs
to capture NTLM authentication from within the network and Linux infrastructure isn't
available, Inveigh via inline_assembly provides that capability. Important: captured hashes
need offline cracking (not a Sage capability) — the primary operational value is relay,
not crack.

## Output
Console output listing captured NTLMv2 hashes per authentication event. File output
(when `-FileOutput Y`) writes hashes to disk for offline processing.
