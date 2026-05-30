---
name: SharpSvc
category: privilege-escalation
subcategories: [service-manipulation, service-binary-hijack, scm-abuse]
tradecraft_tags: [service, scm, binary-hijack, persistence, privilege-escalation, dotnet, apollo-runnable]
mitre_attack:
  - id: T1574.010
    name: Hijack Execution Flow — Services File Permissions Weakness
  - id: T1543.003
    name: Create or Modify System Process — Windows Service
source:
  url: https://github.com/djhohnstein/SharpSvc
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpSvc.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Service creation (Event 7045) and service modification (Event 7040) are audited.
  Reading the service control manager is standard — enumeration is low-noise.
  Binary path modification generates a service configuration change event.
usage_examples:
  - description: List all services with their binary paths
    args: "SharpSvc.exe list"
  - description: Check service binary permissions (find writable ones)
    args: "SharpSvc.exe check"
  - description: Modify a service's binary path (exploitation)
    args: "SharpSvc.exe modify SvcName 'C:\\Windows\\Temp\\payload.exe'"
  - description: Create a new service (requires admin)
    args: "SharpSvc.exe create PayloadSvc 'C:\\Windows\\Temp\\payload.exe'"
  - description: Start/stop a service
    args: "SharpSvc.exe start SvcName"
opsec_notes: |
  SharpSvc wraps Service Control Manager (SCM) operations in a .NET assembly —
  inline_assembly compatible for Apollo. Service binary modification requires
  admin rights (or specific SCM write permissions). Service creation and modification
  generate audit events. For persistence via service, SharPersist is the more
  maintained alternative.
gotchas: |
  Modifying a service's binary path requires WriteServiceConfig permission on the service
  (admin by default, sometimes ACL-writable by lower privileges — SharpUp identifies these).
  The service must be stopped before modifying and restarted afterward. Service modification
  is a persistent artifact — clean up after use. Not actively maintained.
related_ttps: [sharpup, sharpersist, seatbelt]
alternatives: [sharpup-service-check, sharpersist, manual-sc-command]
common_args:
  list:
    description: List all services and their binary paths
    typical_values: [flag-only]
  check:
    description: Check service binary file permissions for writable binaries
    typical_values: [flag-only]
  modify:
    description: Change a service's binary path
    typical_values: ["SvcName 'C:\\\\Windows\\\\Temp\\\\payload.exe'"]
  create:
    description: Create a new service (requires admin)
    typical_values: ["ServiceName 'C:\\\\path\\\\payload.exe'"]
  start:
    description: Start a service
    typical_values: ["ServiceName"]
last_updated: 2026-05-29
---

# SharpSvc

A .NET assembly for Service Control Manager (SCM) enumeration and manipulation.
SharpSvc lists services, checks file permissions on service binaries (identifying
writable targets), and modifies service binary paths. Apollo-compatible via inline_assembly.

## Typical use cases
- Enumerate services and their binary paths for hijack opportunities
- Verify SharpUp's ModifiableServiceBinaries findings before exploitation
- Modify a writable service binary path for privilege escalation or persistence

## How Sage uses this
SharpSvc is used to verify and exploit service binary hijack opportunities identified
by SharpUp. The workflow: SharpUp identifies writable service binaries → SharpSvc
verifies permissions and modifies the path → service restart triggers payload execution.
