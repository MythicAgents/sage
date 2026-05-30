---
name: WMI Query BOF
category: discovery
subcategories: [wmi, in-process-wmi, host-enumeration, bof]
tradecraft_tags: [wmi, query, bof, in-process, enumeration, athena]
mitre_attack:
  - id: T1047
    name: Windows Management Instrumentation
source:
  url: https://github.com/trustedsec/CS-Situational-Awareness-BOF
  license: BSD-3-Clause
  maintained: true
binary_type: bof
binary_filename: wmiquery.x64.o
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  WMI queries from the agent process are low-signal when the query is for standard
  classes (Win32_Process, Win32_ComputerSystem). Queries for unusual WMI classes or
  large-volume enumeration from non-management processes is detectable by behavioral EDR.
  In-process (BOF) WMI queries generate no child process.
usage_examples:
  - description: Query running processes via WMI in-process
    args: "execute-bof wmiquery.x64.o 'SELECT * FROM Win32_Process'"
  - description: Query installed software
    args: "execute-bof wmiquery.x64.o 'SELECT Name,Version FROM Win32_Product'"
  - description: Query service accounts
    args: "execute-bof wmiquery.x64.o 'SELECT Name,StartName FROM Win32_Service'"
  - description: Query computer system info
    args: "execute-bof wmiquery.x64.o 'SELECT * FROM Win32_ComputerSystem'"
opsec_notes: |
  WMI queries via BOF run in-process — no wmic.exe or PowerShell child process.
  The WMI query still goes through the WMI infrastructure (winmgmt service) but
  without a separate process creation event. Preferred over Seatbelt for specific
  targeted WMI queries when process creation must be minimized.
gotchas: |
  Apollo has no BOF runner — requires Athena. For Apollo, use Seatbelt (inline_assembly)
  for equivalent WMI-based enumeration. Win32_Product class should be avoided in
  sensitive environments — it performs an MSI consistency check, generates Event 1033,
  and can trigger repairs. Use Win32_InstalledWin32Program or registry instead.
related_ttps: [trustedsec-bofs, seatbelt, sharpwmi]
alternatives: [seatbelt-processes, sharpwmi-local]
common_args:
  query:
    description: WQL (WMI Query Language) query string
    typical_values: ["SELECT * FROM Win32_Process",
                     "SELECT Name,StartName FROM Win32_Service",
                     "SELECT * FROM Win32_ComputerSystem"]
    required: true
last_updated: 2026-05-29
---

# WMI Query BOF

A BOF for executing arbitrary WMI queries in-process via Athena. Provides the same
WMI access as `wmic.exe` or PowerShell `Get-WmiObject` but without spawning a child
process — the query runs inside the C2 agent's process via the WMI COM API.

## Useful WMI Queries for Post-Exploitation

```wql
-- Process list with owner:
SELECT ProcessId,Name,CommandLine FROM Win32_Process

-- Services with run-as account:
SELECT Name,StartName,State,PathName FROM Win32_Service WHERE State='Running'

-- Logged-on users (requires admin for most):
SELECT * FROM Win32_LoggedOnUser

-- Local user accounts:
SELECT Name,Disabled,PasswordChangeable FROM Win32_UserAccount WHERE Domain='%COMPUTERNAME%'

-- Installed patches:
SELECT HotFixID,InstalledOn FROM Win32_QuickFixEngineering ORDER BY InstalledOn DESC

-- Network adapters and IPs:
SELECT * FROM Win32_NetworkAdapterConfiguration WHERE IPEnabled=TRUE

-- Shares:
SELECT Name,Path,Type FROM Win32_Share

-- Computer system info:
SELECT Manufacturer,Model,TotalPhysicalMemory,Domain FROM Win32_ComputerSystem
```

## Apollo-specific note
BOF — requires Athena. For Apollo, Seatbelt's various checks cover most of this data.
SharpWMI can also execute local WMI queries via inline_assembly.
