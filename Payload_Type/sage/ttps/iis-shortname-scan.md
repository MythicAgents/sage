---
name: IIS ShortName Scanner / Web Application Fingerprinting
category: recon
subcategories: [web-recon, iis-enumeration, hidden-files, fingerprinting]
tradecraft_tags: [iis, web, shortname, enumeration, recon, dotnet, infrastructure]
mitre_attack:
  - id: T1046
    name: Network Service Discovery
source:
  url: https://github.com/irsdl/IIS-ShortName-Scanner
  license: Unknown
  maintained: true
binary_type: .net-assembly
binary_filename: IIS-ShortName-Scanner.exe
supported_os: [windows]
architecture: [x64]
privilege_required: none
network_required: true
detection_signal: |
  IIS ShortName scanning sends many HTTP requests with specific URL patterns to
  the IIS server — detectable by web application firewalls, IDS, and IIS request logging.
  The scanning pattern is distinctive (repeated 404/200 responses with ~ in URLs).
usage_examples:
  - description: Scan IIS for short (8.3) filenames via vulnerability
    args: "IIS-ShortName-Scanner.exe 2 20 https://TARGET/IIS_ShortName_Scanner_optsFile.xml"
  - description: Quick scan for hidden files
    args: "IIS-ShortName-Scanner.exe 1 5 https://TARGET/"
opsec_notes: |
  IIS ShortName enumeration is a reconnaissance technique for discovering hidden files
  and directories on IIS web servers (patched by default in newer IIS, but common in
  older deployments). Not directly relevant to Windows post-exploitation but documented
  for completeness in web-facing attack surfaces. For internal IIS servers (intranet),
  this may reveal admin interfaces or backup files.
gotchas: |
  The IIS ShortName vulnerability (CVE-2010-2730 era) is patched in modern IIS by
  default. The scanner uses HTTP 404/200 response differentiation with tilde (~) URL
  encoding. Web application firewalls will block this. Most useful against legacy IIS
  deployments (IIS 6.0, 7.0) or misconfigured modern IIS.
related_ttps: [powerupsql, sharpcloud]
alternatives: [gobuster, feroxbuster, dirb]
common_args:
  target:
    description: Target IIS server URL
    typical_values: ["https://intranet.domain.local/"]
    required: true
last_updated: 2026-05-29
---

# IIS ShortName Scanner

A .NET tool for enumerating hidden files and directories on IIS web servers via the
8.3 short filename vulnerability. When IIS is configured to allow 8.3 filename access
(common in older deployments), it's possible to enumerate the first 6 characters of
filenames by timing the difference in 404 vs 200 responses for ~ prefixed URLs.

## Typical use cases
- Find hidden admin panels, backup files, or configuration endpoints on IIS servers
- Enumerate internal intranet IIS applications for additional attack surface

## Relevance to Post-Exploitation

Internal IIS web applications (SharePoint, admin panels, legacy apps) often run on
domain-joined servers with privileged service accounts. Discovering hidden endpoints
on internal IIS may reveal:
- Admin interfaces with weak authentication
- Configuration endpoints exposing credentials
- Backup files containing sensitive data

## Apollo-specific note
.NET assembly — Apollo inline_assembly compatible for targeting internal web servers.
