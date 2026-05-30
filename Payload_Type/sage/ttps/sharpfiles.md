---
name: SharpFiles
category: collection
subcategories: [file-collection, file-hunt, data-staging]
tradecraft_tags: [file-hunt, collection, staging, keywords, extension, dotnet, apollo-runnable]
mitre_attack:
  - id: T1005
    name: Data from Local System
  - id: T1083
    name: File and Directory Discovery
source:
  url: https://github.com/fullmetalcache/SharpFiles
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpFiles.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Recursive file enumeration and read operations generate file access events.
  DLP solutions monitoring for bulk file reads or sensitive file access will detect this.
  The operations themselves are standard filesystem calls — low detection signal on
  systems without file auditing.
usage_examples:
  - description: Find files matching keyword patterns under a path
    args: "SharpFiles.exe search 'C:\\Users' 'password,credential,secret' txt,doc,xlsx"
  - description: Search all drives for interesting file extensions
    args: "SharpFiles.exe search 'C:\\' 'password' txt,ini,cfg,xml,json,ps1,bat"
  - description: Find recently modified files
    args: "SharpFiles.exe recent 'C:\\Users\\Administrator' 7"
opsec_notes: |
  SharpFiles is a lightweight file hunting tool for targeted searches. For more
  comprehensive share-based file hunting, Snaffler is preferred. SharpFiles is useful
  for quick local file discovery on a specific machine without the network overhead
  of Snaffler.
gotchas: |
  Not actively maintained. For comprehensive file hunting across shares, use Snaffler.
  SharpFiles is most useful for local file discovery on the compromised machine itself.
related_ttps: [snaffler, sharefinder, credential-hunting-checklist, seatbelt]
alternatives: [snaffler, seatbelt-fileinfo, robocopy-with-filter]
common_args:
  search:
    description: Search for files matching keywords and extensions
    typical_values: [flag-only]
  path:
    description: Root path to search from
    typical_values: ["C:\\\\Users", "C:\\\\", "C:\\\\Program Files"]
    required: true
  keywords:
    description: Comma-separated keywords to match in filenames
    typical_values: ["password,credential,secret", "token,key,api"]
  extensions:
    description: Comma-separated file extensions to include
    typical_values: ["txt,doc,xlsx,pdf,ps1,bat,cfg,ini,xml,json"]
last_updated: 2026-05-29
---

# SharpFiles

A .NET assembly for local file discovery by keyword and extension matching. Searches a
directory tree for files matching specified filename keywords and extensions — a lighter-weight
alternative to Snaffler for local machine file hunting (Snaffler focuses on network shares).

## Typical use cases
- Find credential files on the local machine (password.txt, creds.ini, etc.)
- Discover configuration files with database connection strings
- Find recently modified files (post-incident investigation or exfil targeting)

## How Sage uses this
SharpFiles covers local machine file discovery. Snaffler covers network shares.
Together they provide comprehensive file-based credential hunting on the compromised
host and its accessible shares.

## Output
Text listing of matching files with path, size, and last modified date.
