---
name: BOF.NET
category: defense-evasion
subcategories: [bof-framework, dotnet-in-bof, execution-primitive]
tradecraft_tags: [bof, dotnet, bofnet, clr-in-bof, execution, evasion, athena]
mitre_attack:
  - id: T1620
    name: Reflective Code Loading
source:
  url: https://github.com/CCob/BOF.NET
  license: MIT
  maintained: true
binary_type: bof
binary_filename: BOFNET.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  BOF.NET initializes a CLR inside the C2 agent's process to host .NET assemblies.
  CLR initialization from a non-.NET process (e.g. the C2 agent binary if it is
  a native EXE) is detectable by EDR with CLR monitoring. However, if the C2 agent
  is already .NET-based (Apollo, Athena), the CLR is already present and BOF.NET
  adds no new CLR initialization signal.
usage_examples:
  - description: Initialize BOF.NET in the agent process
    args: "execute-bof BOFNET_Init.x64.o"
  - description: Load a .NET assembly into the BOF.NET host
    args: "execute-bof BOFNET_Load.x64.o <base64-assembly>"
  - description: Execute a method in a loaded .NET assembly
    args: "execute-bof BOFNET_Execute.x64.o <AssemblyName> <ClassName> <MethodName> [args]"
  - description: List loaded assemblies in BOF.NET host
    args: "execute-bof BOFNET_ListAssemblies.x64.o"
  - description: Run Seatbelt through BOF.NET (assembly persists in memory, no reload needed)
    args: "execute-bof BOFNET_Execute.x64.o Seatbelt.Program Main '-group=system'"
opsec_notes: |
  BOF.NET hosts a CLR inside the C2 agent's BOF execution context, allowing .NET
  assemblies to be loaded and executed across multiple calls WITHOUT creating an
  AppDomain or loading/unloading on each execution. The assembly persists in memory
  between calls — Seatbelt loaded once can be called many times with different arguments.
  This is particularly valuable for large assemblies (SharpHound, Seatbelt) where
  repeated inline_assembly calls would repeatedly trigger EDR CLR-load events.
gotchas: |
  Apollo has no BOF runner — BOF.NET requires Athena or another BOF-capable agent.
  BOF.NET's assembly persistence is session-scoped: if the agent dies, loaded assemblies
  are lost. The .NET assembly must be BOF.NET-compatible (standard .NET; the Main method
  or specified entry point must accept string arguments). Some assemblies that use
  Console.ReadLine or interactive mode won't work in BOF.NET's headless context.
  Athena's execute-bof command is the invocation path.
related_ttps: [trustedsec-bofs, outflank-remote-ops-bofs, seatbelt, sharphound, nanodump]
alternatives: [inline-assembly-apollo, inceptor-conversion]
common_args:
  BOFNET_Init:
    name: BOFNET_Init
    description: Initialize the CLR host inside the agent process (run once per session)
    typical_values: [flag-only]
    required: true
  BOFNET_Load:
    name: BOFNET_Load
    description: Load a .NET assembly into the BOF.NET CLR host (persists until agent dies)
    typical_values: ["<base64-assembly-bytes>"]
  BOFNET_Execute:
    name: BOFNET_Execute
    description: Invoke a method on a previously-loaded assembly
    typical_values: ["Seatbelt.Program Main '-group=system'"]
  BOFNET_ListAssemblies:
    name: BOFNET_ListAssemblies
    description: List all currently loaded assemblies in the BOF.NET host
    typical_values: [flag-only]
last_updated: 2026-05-29
---

# BOF.NET

CCob's framework for hosting a .NET CLR inside a Beacon Object File. BOF.NET
initializes a CLR in the C2 agent's process and provides a persistent .NET assembly
host that survives across multiple BOF invocations. The key operational benefit:
a large assembly like Seatbelt or SharpHound can be loaded ONCE and called many
times with different arguments, without the overhead and detection signal of
repeated AppDomain creation/destruction.

## How BOF.NET Changes Execution

| Model | Per-execution events | Assembly lifetime |
|-------|---------------------|------------------|
| inline_assembly (Apollo) | AppDomain create → Load → Unload per call | Single call |
| BOF.NET | CLR init once (BOFNET_Init), then BOFNET_Execute | Persistent in agent process |

## Typical use cases
- Load Seatbelt once, run many different checks without repeated assembly load events
- Run SharpHound multiple times with different collection methods without reloading
- Load Rubeus persistently for repeated ticket operations
- Avoid the AppDomain create/destroy cycle that EDRs watch for

## How Sage uses this
BOF.NET is the most efficient execution model for repeated use of large .NET assemblies.
With Athena, Sage can BOFNET_Init once at session start, then BOFNET_Load each tool
that will be used multiple times during the engagement.

```
# Setup (once per agent session):
execute-bof BOFNET_Init.x64.o

# Load tools that will be used repeatedly:
execute-bof BOFNET_Load.x64.o <seatbelt-base64>
execute-bof BOFNET_Load.x64.o <rubeus-base64>

# Invoke as needed (no reload overhead):
execute-bof BOFNET_Execute.x64.o Seatbelt.Program Main '-group=user'
execute-bof BOFNET_Execute.x64.o Seatbelt.Program Main 'CredEnum WindowsCredentialFiles'
execute-bof BOFNET_Execute.x64.o Rubeus.Program MainString 'triage'
```

## Apollo-specific note
Apollo has no BOF runner. BOF.NET requires Athena's `execute-bof` command.
For Apollo, inline_assembly is the equivalent (though less efficient for repeated calls).
