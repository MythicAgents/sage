---
name: SharpWifi
category: credential-access
subcategories: [wifi-credentials, network-profiles, cleartext-psk]
tradecraft_tags: [wifi, wpa2, psk, network-profile, dotnet, credentials, apollo-runnable]
mitre_attack:
  - id: T1552.001
    name: Unsecured Credentials — Credentials In Files
source:
  url: https://github.com/jaredhaight/SharpWifi
  license: Unknown
  maintained: false
binary_type: .net-assembly
binary_filename: SharpWifi.exe
supported_os: [windows]
architecture: [x64]
privilege_required: user
network_required: false
detection_signal: |
  SharpWifi reads WiFi profile data via netsh wlan (Windows Wireless LAN API).
  The underlying API calls are standard — identical to what netsh.exe does when
  called by a user. Low detection signal. The read itself generates no network traffic.
usage_examples:
  - description: List all saved WiFi networks with passwords
    args: "SharpWifi.exe"
  - description: Equivalent netsh command (built-in Windows)
    args: "netsh wlan show profiles; for each profile: netsh wlan show profile 'SSID' key=clear"
opsec_notes: |
  WiFi credential extraction is a low-noise, high-value operation. Saved WiFi PSKs
  are often the same passwords used for other systems (especially home/small office
  environments). The profiles include corporate WiFi with enterprise credentials (802.1X)
  which may reveal the user's domain password. Detection signal is minimal — standard
  wireless LAN API.
gotchas: |
  WiFi profiles are per-user and per-machine. Machine-level profiles (stored in the
  machine profile store) require admin to read. User profiles are accessible to the
  current user. Enterprise WiFi (802.1X/WPA2-Enterprise) stores the domain username
  but NOT the password (RADIUS authentication) — the PSK-type profiles are the
  ones with cleartext credentials.
related_ttps: [seatbelt, credential-hunting-checklist]
alternatives: [seatbelt-wifiprofiles, netsh-wlan-manual]
common_args: {}
last_updated: 2026-05-29
---

# SharpWifi

A .NET assembly that enumerates all saved WiFi network profiles and extracts their
pre-shared keys (PSKs) in cleartext using the Windows Wireless LAN API. Saved WiFi
passwords often match other system passwords (home router, shared office password,
etc.) and can be reused for lateral movement.

## Typical use cases
- Extract saved WiFi PSKs (often reused as domain or system passwords)
- Identify WiFi networks the target machine has connected to (network intel)
- Find enterprise WiFi configurations (SSID, auth method, certificate info)

## Equivalent Built-in Command

```cmd
REM List all profiles:
netsh wlan show profiles

REM Extract PSK for each (run for each SSID name):
netsh wlan show profile "WiFiNetworkName" key=clear
REM Look for "Key Content" line
```

SharpWifi automates this for all profiles at once.

## Output
One entry per WiFi profile: SSID name, authentication type, and cleartext PSK
(for WPA/WPA2-PSK profiles). Enterprise (802.1X) profiles show username but not password.
