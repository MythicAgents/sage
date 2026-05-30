---
name: SharpChromium
category: credential-access
subcategories: [browser-credentials, dpapi, saved-passwords]
tradecraft_tags: [browser, chromium, chrome, credentials, cookies, history, dpapi]
mitre_attack:
  - id: T1555.003
    name: Credentials from Password Stores — Credentials from Web Browsers
source:
  url: https://github.com/djhohnstein/SharpChromium
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpChromium.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  DPAPI calls for decrypting browser key material are detected by behavioral EDR when
  coming from non-browser processes. File access to Chrome/Chromium profile directories
  (AppData\Local\Google\Chrome\User Data\) is detectable by Sysmon file monitoring.
  Some EDRs specifically monitor for browser credential database access from non-browser
  processes.
usage_examples:
  - description: Extract all Chromium saved passwords
    args: "logins"
  - description: Extract all Chromium cookies
    args: "cookies"
  - description: Extract browser history
    args: "history"
  - description: Target a specific Chromium-based browser
    args: "logins --browser edge"
opsec_notes: |
  Browser credential extraction is valuable but noisy from an EDR perspective — DPAPI
  calls from non-browser processes accessing browser databases are flagged by CrowdStrike
  and SentinelOne. Run from a SYSTEM context or from the user's own session to reduce
  the cross-process aspect. SharpDPAPI's `credentials /target:chrome` provides the same
  functionality with GhostPack's reputation.
gotchas: |
  Browser must not be running with the profile files locked (SQLite locking prevents
  access). Log the user out or work with shadow copies. SharpChromium is not actively
  maintained — test against the current Chrome version as the key derivation method
  has changed (v80+: DPAPI-bound AES key in Local State file). Prefer SharpDPAPI for
  maintained DPAPI-based credential extraction.
related_ttps: [sharpdpapi, seatbelt, snaffler]
alternatives: [sharpdpapi, lazagne, mimikatz-dpapi]
common_args:
  logins:
    description: Extract saved login credentials (username + password)
    typical_values: [flag-only]
    required: false
  cookies:
    description: Extract browser cookies (session tokens, auth cookies)
    typical_values: [flag-only]
  history:
    description: Extract browsing history
    typical_values: [flag-only]
  --browser:
    description: Target a specific browser
    typical_values: [chrome, edge, brave, opera]
last_updated: 2026-05-29
---

# SharpChromium

A .NET assembly for extracting saved credentials, cookies, and history from Chromium-based
browsers (Chrome, Edge, Brave, Opera). Uses DPAPI to decrypt the browser's saved password
database. Useful for finding web application credentials (VPN portals, cloud consoles,
internal tools) that domain users have saved in their browsers.

## Typical use cases
- Extract saved VPN/web app/SaaS passwords from a compromised user's Chrome profile
- Harvest session cookies for session hijacking (pass-the-cookie attacks)
- Browser history for intelligence on internal services and access patterns

## How Sage uses this
SharpChromium is a post-foothold collection step for user-context credential harvesting.
Sage may run this after gaining access to a user machine to collect web credentials
before the user's session expires. Note that SharpDPAPI with `/target:chrome` is the
more maintained alternative.

## Output
Text listing of credentials (URL, username, password) and/or cookie data to stdout.
