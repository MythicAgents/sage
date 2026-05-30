---
name: PowerUpSQL
category: lateral-movement
subcategories: [mssql-abuse, sql-server-exploitation, linked-server, xp-cmdshell]
tradecraft_tags: [mssql, sql-server, xp-cmdshell, linked-servers, impersonation, powershell]
mitre_attack:
  - id: T1505.001
    name: Server Software Component — SQL Stored Procedures
source:
  url: https://github.com/NetSPI/PowerUpSQL
  license: BSD-3-Clause
  maintained: true
binary_type: powershell-script
binary_filename: PowerUpSQL.ps1
supported_os: [windows]
architecture: [x64, x86]
privilege_required: domain-user
network_required: true
detection_signal: |
  SQL Server audit logs capture xp_cmdshell execution (enabled by auditing). SQL agent
  jobs creating arbitrary processes generate Windows Event Log entries. Linked server
  traversal generates authentication events on each SQL instance. Network connections
  to TCP 1433 (default SQL) from unusual sources.
usage_examples:
  - description: Discover SQL servers in the domain via LDAP
    args: "Get-SQLInstanceDomain"
  - description: Check SQL access for current user
    args: "Get-SQLInstanceDomain | Get-SQLConnectionTest"
  - description: Enable xp_cmdshell on accessible SQL server (requires sysadmin)
    args: "Invoke-SQLOSCmd -Instance SQLSRV01.north.sevenkingdoms.local -Command 'whoami'"
  - description: Traverse linked servers for privilege escalation
    args: "Get-SQLServerLinkCrawl -Instance SQLSRV01 -Query 'exec master..xp_cmdshell ''whoami'''"
  - description: Find impersonation opportunities
    args: "Invoke-SQLAuditPrivImpersonateLogin -Instance SQLSRV01"
opsec_notes: |
  SQL Server xp_cmdshell execution is logged if SQL auditing is configured. Enabling
  xp_cmdshell changes a SQL server's configuration (modifies sp_configure) — this is
  persistent and visible to DBAs. The linked server traversal technique can chain SQL
  servers to reach higher-privilege instances without direct domain-level access. SQL
  Agent job abuse doesn't require xp_cmdshell.
gotchas: |
  PowerShell script — requires AMSI bypass first. xp_cmdshell requires sysadmin role or
  explicit EXECUTE grant. Most modern SQL installations have xp_cmdshell disabled; check
  for public execute rights or SA-impersonation before trying to enable it. Linked server
  attacks are more stealthy since they don't modify server configuration. Undo xp_cmdshell
  enablement after use.
related_ttps: [powerview, seatbelt, crackmapexec, sharphound]
alternatives: [heidiSQL-manual, impacket-mssqlclient]
common_args:
  Get-SQLInstanceDomain:
    description: Discover SQL Server instances via domain LDAP/SPN enumeration
    typical_values: [flag-only]
  Get-SQLConnectionTest:
    description: Test connectivity and auth to a SQL instance
    typical_values: [flag-only]
  Invoke-SQLOSCmd:
    description: Execute OS commands via xp_cmdshell
    typical_values: [flag-only]
  Get-SQLServerLinkCrawl:
    description: Traverse linked servers and execute queries
    typical_values: [flag-only]
  -Instance:
    description: Target SQL Server instance (FQDN or FQDN\Instance)
    typical_values: ["SQLSRV01.north.sevenkingdoms.local", "SQLSRV01\\SQLEXPRESS"]
    required: true
  -Command:
    description: OS command to execute via xp_cmdshell
    typical_values: ["whoami", "net user"]
last_updated: 2026-05-29
---

# PowerUpSQL

NetSPI's PowerShell SQL Server attack toolkit. PowerUpSQL enumerates SQL Server instances
via LDAP SPN discovery, tests access with current credentials, identifies privilege
escalation paths (impersonation, xp_cmdshell, linked servers), and enables OS command
execution from compromised SQL contexts. SQL servers often run under service accounts
with high domain privileges — compromising them can yield domain escalation.

## Typical use cases
- Enumerate SQL Server instances in the domain (LDAP SPN discovery)
- Test current user access level on discovered instances
- Execute OS commands via xp_cmdshell on accessible sysadmin instances
- Chain linked servers to reach higher-privilege SQL instances
- Escalate via EXECUTE AS (impersonation) on SQL logins

## How Sage uses this
SharpHound may identify SQL Server instances via SPN data. PowerUpSQL's LDAP discovery
(`Get-SQLInstanceDomain`) finds them comprehensively. When a domain user has access
to a SQL server running as a privileged service account, xp_cmdshell or linked server
traversal provides lateral movement and/or privilege escalation.

## Output
PowerShell object output — connection test results, command output, linked server paths.
