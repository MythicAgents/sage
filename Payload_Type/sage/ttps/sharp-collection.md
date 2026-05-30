---
name: SharpCollection
category: collection
subcategories: [file-harvest, credential-collection, data-staging]
tradecraft_tags: [collection, file-hunt, staging, data-theft, dotnet]
mitre_attack:
  - id: T1005
    name: Data from Local System
  - id: T1039
    name: Data from Network Shared Drive
source:
  url: https://github.com/Flangvik/SharpCollection
  license: Unknown
  maintained: true
binary_type: .net-assembly
binary_filename: (various)
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  File access patterns — reading many files from sensitive directories generates file
  access events. Some collection tools create compressed archives (zip creation events).
  Staging large amounts of data in temp directories is detectable by DLP solutions.
usage_examples:
  - description: SharpCollection is a repository of pre-compiled .NET tools
    args: "(not a single tool — repository of pre-built .NET assemblies)"
  - description: Download pre-compiled SharpHound, Rubeus, etc. for staging
    args: "(use SharpCollection's GitHub Releases for pre-built binaries)"
opsec_notes: |
  SharpCollection is a curated repository of pre-compiled .NET security tools, not
  a collection tool itself. It provides pre-built binaries of common tools (SharpHound,
  Rubeus, Seatbelt, etc.) for operators who don't want to compile from source.
  Importantly: pre-compiled binaries from public repos have KNOWN HASHES — EDR vendors
  have these hashes. ALWAYS compile from source for operational use.
gotchas: |
  SharpCollection binaries are publicly known and signatured. DO NOT use them in
  production engagements — their hashes are in every EDR vendor's signature database.
  They are useful for lab testing and verifying tool functionality. For operational use,
  compile tools from source and optionally obfuscate.
related_ttps: [sharphound, rubeus, seatbelt, sharpup, snaffler]
alternatives: [compile-from-source, custom-obfuscated-build]
common_args: {}
last_updated: 2026-05-29
---

# SharpCollection

Flangvik's repository of pre-compiled .NET offensive security tools targeting various
.NET framework versions (net35, net40, net45, netstandard2.0). Provides ready-to-use
binaries of SharpHound, Rubeus, Seatbelt, SharpUp, and many others. Used primarily
for lab/testing purposes.

## Warning: Hash Signatured
**All SharpCollection binaries are publicly known and have well-established hashes
in EDR vendor signature databases.** Do not use in production engagements — compile
from source for operational use.

## Typical use cases
- Lab and testing: quickly obtain compiled versions of tools for verification
- Verifying tool functionality before source compilation
- Reference for which .NET version targets each tool supports

## How Sage uses this
Reference only — for operational deployments, all tools should be compiled from source.
